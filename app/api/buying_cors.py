"""Sites origins apply only to Buying; existing public/Tilda policy is preserved."""
from starlette.middleware.cors import CORSMiddleware


class BuyingCORSMiddleware:
    def __init__(self, app, buying_origins, **kwargs):
        self.public = CORSMiddleware(app, **kwargs)
        self.buying = CORSMiddleware(app, **{**kwargs, 'allow_origins': buying_origins})

    async def __call__(self, scope, receive, send):
        handler = self.buying if scope.get('path', '').startswith('/buying/') else self.public
        await handler(scope, receive, send)
