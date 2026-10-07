from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]

def test_compose_and_images_define_secure_runtime_contract() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"db", "embeddings", "api", "web"}
    assert "pgvector" in services["db"]["image"]
    assert all("healthcheck" in services[name] for name in services)
    assert services["api"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["embeddings"]["condition"] == "service_healthy"
    rendered = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "MODEL_API_KEY" in rendered and "replace-me" not in rendered
    assert "uploads" in services["api"]["volumes"][0]
    assert services["embeddings"]["volumes"][0]["type"] == "bind"
    assert services["embeddings"]["volumes"][0]["read_only"] is True
    assert (
        services["embeddings"]["environment"]["LOCAL_MODEL_THREADS"]
        == "${LOCAL_MODEL_THREADS:-0}"
    )
    for dockerfile in (
        ROOT / "backend" / "Dockerfile",
        ROOT / "embedding-service" / "Dockerfile",
        ROOT / "frontend" / "Dockerfile",
    ):
        text = dockerfile.read_text(encoding="utf-8")
        assert "USER " in text and "USER root" not in text

def test_seed_script_requires_environment_passwords_and_is_idempotent() -> None:
    text = (ROOT / "scripts" / "seed_demo.py").read_text(encoding="utf-8")
    assert "DEMO_ADMIN_PASSWORD" in text and "DEMO_EMPLOYEE_PASSWORD" in text
    assert "change-me" not in text and "password123" not in text
    assert "select(User)" in text

def test_nginx_sets_browser_security_headers() -> None:
    text = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    for header in ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Permissions-Policy"):
        assert header in text
    assert "unsafe-eval" not in text and "unsafe-inline" not in text


def test_vite_does_not_inline_font_assets_blocked_by_the_nginx_csp() -> None:
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    vite = (ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")

    assert "font-src 'self'" in nginx
    assert "font-src 'self' data:" not in nginx
    assert "assetsInlineLimit: 0" in vite


def test_api_image_installs_headless_office_before_non_root_runtime() -> None:
    text = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    install = text.index("apt-get install")
    user = text.index("USER policy")
    assert install < user
    for package in ("libreoffice-core-nogui", "libreoffice-writer-nogui", "fonts-noto-cjk"):
        assert package in text
    assert "--no-install-recommends" in text
    assert "rm -rf /var/lib/apt/lists/*" in text


def test_compose_passes_the_complete_rag_v2_runtime_configuration() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["api"]["environment"]

    assert environment["QUERY_VARIANT_MAX_COUNT"] == "${QUERY_VARIANT_MAX_COUNT:-3}"
    assert environment["MODEL_DISABLE_THINKING"] == "${MODEL_DISABLE_THINKING:-false}"
    assert environment["RETRIEVAL_LEXICAL_CANDIDATE_LIMIT"] == "${RETRIEVAL_LEXICAL_CANDIDATE_LIMIT:-20}"
    assert environment["RETRIEVAL_VECTOR_CANDIDATE_LIMIT"] == "${RETRIEVAL_VECTOR_CANDIDATE_LIMIT:-20}"
    assert environment["RETRIEVAL_FUSED_TOP_K"] == "${RETRIEVAL_FUSED_TOP_K:-12}"
    assert environment["RETRIEVAL_ALLOW_VECTOR_ONLY_FALLBACK"] == "${RETRIEVAL_ALLOW_VECTOR_ONLY_FALLBACK:-false}"
    assert environment["EVIDENCE_MAX_CHUNKS"] == "${EVIDENCE_MAX_CHUNKS:-6}"
    assert environment["EVIDENCE_LEXICAL_THRESHOLD"] == "${EVIDENCE_LEXICAL_THRESHOLD:-0.28}"
    assert environment["EVIDENCE_SEMANTIC_THRESHOLD"] == "${EVIDENCE_SEMANTIC_THRESHOLD:-0.78}"
    assert environment["EVIDENCE_DUAL_CHANNEL_MINIMUM"] == "${EVIDENCE_DUAL_CHANNEL_MINIMUM:-0.20}"
    assert "EVIDENCE_THRESHOLD" not in environment


def test_readme_requires_explicit_provider_capability_for_thinking_control() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "MODEL_DISABLE_THINKING=true" in text
    assert "DeepSeek" in text
    assert "OpenAI-compatible" in text
    assert "defaults to `false`" in text


def test_readme_documents_safe_pg_trgm_upgrade_and_reversible_governance() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    for required in (
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "pg_dump -Fc",
        "pg_restore --list",
        "alembic upgrade head",
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT",
        "/api/v1/documents/{document_id}/disable",
        "/api/v1/documents/{document_id}/enable",
        "真实DOCX格式验收.pdf",
        "真实DOCX格式验收.docx",
        "信息与版本说明.txt",
        "火星出差管理办法（虚构演示）-录制A.txt",
        "is_enabled",
        "DocumentStatus.DISABLED",
    ):
        assert required in text
    assert "exact UUID" in text
    assert "Do not delete" in text
