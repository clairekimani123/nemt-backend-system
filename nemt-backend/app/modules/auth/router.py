from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_tenant_db
from app.core.security import create_mfa_token
from app.modules.auth import schemas, service
from app.modules.users.models import User
from app.modules.users.schemas import UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: schemas.RegisterIn, request: Request, db: AsyncSession = Depends(get_tenant_db)):
    """Passenger self-registration. Staff accounts are created by a Company Admin."""
    return await service.register_passenger(db, db.info["tenant_id"], body, request)


@router.post("/login", response_model=schemas.LoginOut)
async def login(body: schemas.LoginIn, request: Request, db: AsyncSession = Depends(get_tenant_db)):
    company_id = db.info["tenant_id"]
    user = await service.authenticate(db, company_id, body.email, body.password, request)
    if user.mfa_enabled:
        await db.commit()
        return schemas.LoginOut(mfa_required=True, mfa_token=create_mfa_token(user.id, company_id))
    tokens = await service.complete_login(db, user, request)
    return schemas.LoginOut(**tokens.model_dump())


@router.post("/mfa/login", response_model=schemas.TokenOut)
async def mfa_login(body: schemas.MfaLoginIn, request: Request, db: AsyncSession = Depends(get_tenant_db)):
    return await service.mfa_login(db, db.info["tenant_id"], body.mfa_token, body.code, request)


@router.post("/refresh", response_model=schemas.TokenOut)
async def refresh(body: schemas.RefreshIn, request: Request):
    return await service.rotate_refresh_token(body.refresh_token, request)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: schemas.RefreshIn):
    await service.logout(body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: schemas.ForgotIn, request: Request, db: AsyncSession = Depends(get_tenant_db)):
    await service.forgot_password(db, db.info["tenant_id"], body.email, request)
    return {"detail": "If that account exists, an email is on its way."}


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(body: schemas.ResetIn, request: Request):
    await service.reset_password(body.token, body.new_password, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/mfa/setup", response_model=schemas.MfaSetupOut)
async def mfa_setup(
    request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    secret, uri = await service.mfa_setup(db, user, request)
    return schemas.MfaSetupOut(secret=secret, otpauth_uri=uri)


@router.post("/mfa/enable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_enable(
    body: schemas.MfaEnableIn, request: Request,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    await service.mfa_enable(db, user, body.code, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)