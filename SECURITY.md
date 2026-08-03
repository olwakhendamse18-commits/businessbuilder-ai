# Security policy and deployment baseline

## Required production configuration

- Set unique `SECRET_KEY` and `ENCRYPTION_KEY` values through the deployment secret store.
- Set `PUBLIC_BASE_URL` to the canonical HTTPS origin and `TRUSTED_HOSTS` to the accepted hostnames.
- Use PostgreSQL through `DATABASE_URL` and deploy web initialization before the worker.
- Keep `VOICE_RUNTIME_ENABLED`, `BROWSER_CONTROL_ENABLED`, and `RESEARCH_USE_OPENAI_BACKGROUND_MODE` false until their separate rollout gates pass.
- Configure `UPLOAD_STORAGE_DIR` only to a durable, private storage mount. Uploads fail closed in production when durable storage is absent.
- Deploy the web service and the single generic worker from the same reviewed commit.

## Application protections

- Unsafe browser requests require a session CSRF token or strict same-origin proof.
- State-changing application routes use non-GET methods.
- Session cookies are HTTP-only, SameSite=Lax, and Secure in production.
- Responses include content-type, framing, referrer, permissions, CSP, and production HSTS headers.
- User and model chat messages are inserted into the DOM as text, never executable HTML.
- Uploads are size limited, type checked, isolated by user, and stored under random server-generated names.
- Authentication failures are rate limited. Production deployments should replace the process-local limiter with a shared store before scaling the web service beyond one instance.

## Validation

Run before merging:

```text
python -m pip install -r requirements-dev.txt
ruff check --select E9,F63,F7,F82 .
pip-audit -r requirements.lock
python -m unittest discover -s tests -v
```

Do not commit `.env`, databases, uploads, browser artifacts, or runtime logs. Report suspected credentials privately and rotate them before removing them from repository history.
