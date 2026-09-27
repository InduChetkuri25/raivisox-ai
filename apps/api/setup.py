"""Setup script for RAIVISOX API."""

from setuptools import setup, find_packages

setup(
    name="raivisox-api",
    version="0.1.0",
    description="RAIVISOX AI API - Next-Gen Adaptive Digital Intelligence",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "fastapi>=0.115.0",
        "uvicorn[standard]>=0.27.0",
        "pydantic>=2.0.0",
        "pydantic-settings>=2.0.0",
        "sqlalchemy>=2.0.0",
        "asyncpg>=0.28.0",
        "alembic>=1.13.0",
        "python-jose[cryptography]>=3.3.0",
        "passlib[bcrypt]>=1.7.4",
        "python-multipart>=0.0.6",
        "httpx>=0.27.0",
        "redis>=5.0.0",
        "structlog>=24.0.0",
    ],
)
