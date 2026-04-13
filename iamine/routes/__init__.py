"""Route registration for IAMINE pool."""

from fastapi import FastAPI


def register_routes(app: FastAPI):
    """Enregistre tous les routers sur l'application FastAPI."""
    from .status import router as status_router
    from .inference import router as inference_router
    from .conversations import router as conversations_router
    from .auth import router as auth_router
    from .admin import router as admin_router
    from .static import router as static_router
    from .websocket import router as websocket_router
    from .dev import router as dev_router
    # from .red import router as red_router  # RED admin desactive
    from .anthropic import router as anthropic_router
    from .jobs import router as jobs_router
    from .federation import router as federation_router
    from .memory import router as memory_router

    app.include_router(status_router)
    app.include_router(inference_router)
    app.include_router(conversations_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(static_router)
    app.include_router(websocket_router)
    app.include_router(dev_router)
    # app.include_router(red_router)  # RED admin desactive
    app.include_router(anthropic_router)
    app.include_router(jobs_router)
    app.include_router(federation_router)
    app.include_router(memory_router)
