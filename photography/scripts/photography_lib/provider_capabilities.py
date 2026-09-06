"""Conservative adapter support; not a claim of account-level model access."""


def capabilities(settings):
    official = settings["base_url"].rstrip("/") == "https://api.openai.com/v1"
    verified_model = settings["model"] in ("gpt-5.4-mini", "gpt-5.4-mini-2026-03-17")
    direct = settings["provider"] == "openai"
    return dict(immediate=True, batch=direct and official and verified_model,
        multi_image=direct and official and verified_model,
        context_tokens=400000 if verified_model else None,
        account_access="unverified", limits_source="user_settings_or_unknown",
        checked_at="2026-09-05", source="https://developers.openai.com/api/docs/models/gpt-5.4-mini",
        note="Only documented models on the official endpoint enable Batch/multi-image. Other models retain single-image requests; capability support must be added explicitly.")
