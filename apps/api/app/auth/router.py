"""Authentication router."""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.auth import service as auth_service
from app.auth.schemas import UserCreate, UserLogin, Token, UserResponse, UserUpdate
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["Authentication"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    user_data: UserCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserResponse:
    """Register a new user."""
    user = await auth_service.create_user(db, user_data)
    return auth_service.to_user_response(user)


@router.post("/login", response_model=Token)
async def login(
    credentials: UserLogin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Token:
    """Login and get an access token."""
    user = await auth_service.authenticate_user(db, credentials.email, credentials.password)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token, expires_in = auth_service.create_access_token(
        data={
            "sub": user.id,
            "email": user.email,
        },
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )

    return Token(
        access_token=access_token,
        token_type="bearer",
        expires_in=expires_in,
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(oauth2_scheme)],
) -> UserResponse:
    """Get the current authenticated user's information."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await auth_service.get_current_user(db, token)
    return auth_service.to_user_response(user)


@router.put("/me", response_model=UserResponse)
async def update_current_user(
    user_data: UserUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(oauth2_scheme)],
) -> UserResponse:
    """Update the current user's information."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await auth_service.get_current_user(db, token)

    update_data = user_data.model_dump(exclude_unset=True)

    # Check if email is being changed
    if "email" in update_data and update_data["email"] != user.email:
        from sqlalchemy import select

        result = await db.execute(
            select(User).where(User.email == update_data["email"])
        )
        existing_user = result.scalar_one_or_none()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already in use",
            )

    for field, value in update_data.items():
        setattr(user, field, value)

    await db.commit()
    await db.refresh(user)

    return auth_service.to_user_response(user)
