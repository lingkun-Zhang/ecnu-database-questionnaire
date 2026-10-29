"""One test-session URI policy shared by API and database fixtures."""


def resolve_test_mongo_uri(environment, fallback):
    """Explicit canonical name wins; retain the old alias before local config.

    The session fixture freezes the result before Settings loads .env. An
    explicit legacy test URI therefore cannot later switch to a local URI.
    Empty explicit values are errors, not permission to connect elsewhere.
    """
    for name in ("MONGO_URI", "MONGODB_URI"):
        if name in environment:
            value = environment[name]
            if not value or not value.strip():
                raise ValueError("An explicitly configured test MongoDB URI is empty")
            return value
    return fallback()
