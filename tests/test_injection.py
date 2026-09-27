"""Dependency injection and principal resolution."""

from __future__ import annotations

import asyncio

import pytest
from conftest import make_context, make_dispatch, make_update

from vitrine.auth import Auth, admin_only, requires, requires_principal
from vitrine.exceptions import (
    BannedError,
    InjectionError,
    NotAuthorizedError,
    NotRegisteredError,
)
from vitrine.injection import (
    _MISSING,
    DependencyScope,
    Depends,
    Invocation,
    Providers,
    resolve_kwargs,
    run_in_update_scope,
)
from vitrine.routing import Registration


async def test_reserved_names_and_providers():
    providers = Providers()
    providers.register_value("config", {"env": "test"})
    providers.register("greeting", lambda: "hello")

    async def handler(update, context, config, greeting, missing="fallback"):
        return update, context, config, greeting, missing

    update, context = make_update(text="hi"), make_context()
    inv = Invocation(update=update, context=context, handler_name="h")
    kwargs = await resolve_kwargs(handler, inv, providers)

    assert kwargs == {
        "update": update,
        "context": context,
        "config": {"env": "test"},
        "greeting": "hello",
        "missing": "fallback",
    }


async def test_recursive_providers_cached_per_invocation():
    created = []
    providers = Providers()

    async def db():
        created.append("db")
        return {"pool": True}

    providers.register("db", db)
    providers.register("repo", lambda db: {"repo": True, "db": db})

    async def handler(db, repo):
        return db, repo

    inv = Invocation(handler_name="h")
    kwargs = await resolve_kwargs(handler, inv, providers)

    assert kwargs["repo"]["db"] is kwargs["db"]
    assert created == ["db"]  # resolved once despite two consumers


async def test_generator_provider_cleanup():
    events = []
    providers = Providers()

    async def session():
        events.append("open")
        yield "SESSION"
        events.append("close")

    providers.register("session", session)

    async def handler(session):
        events.append(f"use:{session}")

    inv = Invocation(handler_name="h")
    kwargs = await resolve_kwargs(handler, inv, providers)
    await handler(**kwargs)

    assert events == ["open", "use:SESSION"]

    await inv.aclose()

    assert events == ["open", "use:SESSION", "close"]


def session_provider(events: list[str]):
    async def session():
        events.append("open")
        yield "SESSION"
        events.append("close")

    return session


async def test_an_invocation_that_outlives_its_update_holds_the_scope(fake_bot):
    """A handler PTB runs as a task can join the update's scope and still be
    resolving after the update is processed: its cleanups must still run."""
    events: list[str] = []
    providers = Providers()
    providers.register("session", session_provider(events))
    dispatch = make_dispatch(fake_bot, providers)
    update = make_update(text="hi")
    resume = asyncio.Event()
    tasks: list[asyncio.Task] = []

    async def handler(session):
        events.append(f"use:{session}")

    async def late():
        inv = dispatch.invocation("late", update)
        try:
            await resume.wait()  # the update's processing ends meanwhile
            await handler(**await resolve_kwargs(handler, inv, providers))
        finally:
            await inv.aclose()

    async def process():
        tasks.append(asyncio.create_task(late()))
        await asyncio.sleep(0)  # it joins the scope

    await run_in_update_scope(update, process)
    resume.set()
    await tasks[0]

    assert events == ["open", "use:SESSION", "close"]


async def test_a_closed_scope_refuses_providers_it_could_not_clean_up():
    providers = Providers()
    providers.register("session", session_provider([]))
    scope = DependencyScope()
    await scope.aclose()

    async def handler(session): ...

    with pytest.raises(InjectionError, match="scope is closed"):
        await resolve_kwargs(handler, Invocation(scope=scope), providers)


async def test_a_closed_scope_lets_go_of_what_it_resolved():
    """A conversation's timeout timer keeps the update's scope for up to its
    whole timeout; it mustn't keep the providers and principal with it."""
    providers = Providers()
    providers.register("repo", lambda: {"rows": [1, 2, 3]})
    inv = Invocation()
    inv.scope.principal = {"id": 1}

    async def handler(repo): ...

    await resolve_kwargs(handler, inv, providers)
    await inv.aclose()

    assert inv.scope.cache == {}
    assert inv.scope.principal is _MISSING  # resolved afresh if asked again


async def test_depends_marker():
    async def make_token():
        return "tok"

    async def handler(anything=Depends(make_token)):
        return anything

    kwargs = await resolve_kwargs(handler, Invocation(handler_name="h"), Providers())
    assert kwargs == {"anything": "tok"}


async def test_unknown_param_raises():
    async def handler(nonexistent):
        pass

    with pytest.raises(InjectionError, match="nonexistent"):
        await resolve_kwargs(handler, Invocation(handler_name="h"), Providers())


async def test_circular_dependency_detected():
    providers = Providers()
    providers.register("a", lambda b: b)
    providers.register("b", lambda a: a)

    async def handler(a):
        pass

    with pytest.raises(InjectionError, match="circular"):
        await resolve_kwargs(handler, Invocation(handler_name="h"), providers)


# --------------------------------------------------------------------- principal


class Principal:
    def __init__(self, roles=(), banned=False):
        self.id = 7
        self.roles = set(roles)
        self.banned = banned


def make_auth(principal, counter):
    async def resolver(update):
        counter.append(update)
        return principal

    return Auth(
        resolver,
        name="user",
        roles=lambda p: p.roles,
        is_banned=lambda p: p.banned,
    )


async def test_principal_resolved_once_per_update_and_injected(fake_bot):
    counter: list = []
    principal = Principal(roles={"admin"})
    auth = make_auth(principal, counter)
    seen = []

    @requires("admin")
    async def handler(user, update):
        seen.append(user)

    reg = Registration(kind="message", fn=handler, name="h")
    dispatch = make_dispatch(fake_bot, auth=auth)
    update, context = make_update(text="x"), make_context(fake_bot)
    await dispatch.run(reg, update, context)

    assert seen == [principal]
    # guard check + injection + anything else: still exactly one resolution
    assert len(counter) == 1


async def test_requires_blocks_missing_role(fake_bot):
    auth = make_auth(Principal(roles={"user"}), [])
    ran = []

    @requires("moderator")
    async def handler(update):
        ran.append(1)

    reg = Registration(kind="message", fn=handler, name="h")
    dispatch = make_dispatch(fake_bot, auth=auth)
    update = make_update(text="x")
    await dispatch.run(reg, update, make_context(fake_bot))

    assert ran == []
    # friendly UX went to the user instead of a crash
    assert update.effective_message.replies


def test_guard_checks_directly():
    auth = make_auth(Principal(roles={"support"}), [])

    @requires("support")
    async def ok(update):
        pass

    @admin_only
    async def admins(update):
        pass

    auth.check(ok, Principal(roles={"support"}))
    with pytest.raises(NotAuthorizedError):
        auth.check(admins, Principal(roles={"support"}))
    with pytest.raises(BannedError):
        auth.check(ok, Principal(roles={"support"}, banned=True))


async def test_requires_principal_blocks_unregistered(fake_bot):
    auth = make_auth(None, [])  # resolver finds nobody: caller never sent /start
    ran = []

    @requires_principal
    async def handler(update):
        ran.append(1)

    reg = Registration(kind="message", fn=handler, name="h")
    dispatch = make_dispatch(fake_bot, auth=auth)
    update = make_update(text="x")
    await dispatch.run(reg, update, make_context(fake_bot))

    assert ran == []
    assert update.effective_message.replies


async def test_requires_principal_passes_registered(fake_bot):
    auth = make_auth(Principal(), [])
    ran = []

    @requires_principal
    async def handler(update):
        ran.append(1)

    reg = Registration(kind="message", fn=handler, name="h")
    dispatch = make_dispatch(fake_bot, auth=auth)
    await dispatch.run(reg, make_update(text="x"), make_context(fake_bot))

    assert ran == [1]


def test_requires_principal_check_directly():
    auth = make_auth(None, [])

    @requires_principal
    async def handler(update):
        pass

    auth.check(handler, Principal())
    with pytest.raises(NotRegisteredError):
        auth.check(handler, None)


def test_role_guards_report_unregistered_not_unauthorized():
    auth = make_auth(None, [])

    @requires("moderator")
    async def moderate(update):
        pass

    @admin_only
    async def administrate(update):
        pass

    with pytest.raises(NotRegisteredError):
        auth.check(moderate, None)
    with pytest.raises(NotRegisteredError):
        auth.check(administrate, None)
