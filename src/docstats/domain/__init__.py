"""Domain modules for the referral management platform.

Each module here is a thin orchestration layer over ``docstats.storage_base``.
Domain modules must stay free of FastAPI-specific dependencies where possible
so they remain testable in isolation; FastAPI dependencies live in routes.
"""
