from dataclasses import dataclass, field
import hmac
from typing import Literal

import jwt
from fastapi import HTTPException, Security, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


@dataclass(frozen=True)
class Identity:
    company_id: int
    user_id: int
    name: str
    designation: str
    scopes: tuple[str, ...]
    can_export: bool = True
    data_source: Literal['fictional_sample', 'effortrak_backend'] = 'fictional_sample'
    policy_version: str = ''
    credential: str = field(default='', repr=False, compare=False)
    modules: tuple[str, ...] = ('attendance',)

    @property
    def policy_key(self) -> str:
        return f'{self.data_source}:{self.scopes}:{self.can_export}:{self.policy_version}:{self.modules}'


# These credentials represent fictional accounts; never use them with live data.
DEMO_ACCOUNTS = {
    'demo-company-a-employee': Identity(1, 101, 'Asha Demo', 'Employee', ('self',)),
    'demo-company-a-manager': Identity(1, 100, 'Mira Demo', 'Manager', ('self', 'team')),
    'demo-company-a-admin': Identity(1, 103, 'Arun Demo', 'Company admin', ('self', 'team', 'company')),
    'demo-company-b-employee': Identity(2, 201, 'Ben Demo', 'Employee', ('self',)),
    'demo-company-a-no-export': Identity(1, 101, 'Asha Demo', 'Employee', ('self',), False),
}

bearer = HTTPBearer(auto_error=False)


async def authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer),
) -> Identity:
    config = request.app.state.config
    if config.data_mode == 'backend':
        secret = config.ai_service_secret.get_secret_value()
        if not credentials or not hmac.compare_digest(request.headers.get('X-AI-Service-Key', ''), secret):
            raise HTTPException(401, 'Backend mode requires authenticated CodeIgniter delegation.')
        token = credentials.credentials
        try:
            claims = jwt.decode(token, secret, algorithms=['HS256'], issuer='effortrak', audience='effortrak-ai',
                                options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'jti', 'company_id', 'purpose']})
            if (claims['purpose'] != 'attendance-chat' or not claims['sub'].isdigit()
                    or int(claims['sub']) <= 0 or type(claims['company_id']) is not int or claims['company_id'] <= 0
                    or type(claims['iat']) is not int or type(claims['exp']) is not int
                    or not 0 < claims['exp'] - claims['iat'] <= 120):
                raise ValueError('Invalid identity claims')
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError):
            raise HTTPException(401, 'Invalid or expired delegated credential.') from None
        return await request.app.state.backend.identity(token, claims['company_id'], int(claims['sub']))
    identity = DEMO_ACCOUNTS.get(credentials.credentials) if credentials else None
    if identity is None:
        raise HTTPException(401, 'Invalid or missing demo bearer token.', headers={'WWW-Authenticate': 'Bearer'})
    return identity
