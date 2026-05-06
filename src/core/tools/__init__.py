"""Tool implementations.

The transport boundary lives in ``core/platforms/_delegate.py`` — that's
where ``adcp.server.serve()`` routes typed AdCP requests through the
``LazyPlatformRouter`` to the per-tenant platform method, which calls the
``_impl`` functions defined in this package directly. There are no
flat-param wrappers; the SDK validates against the spec types and the
delegate hands the typed request straight through.
"""
