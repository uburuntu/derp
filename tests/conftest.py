import os

# Silence LogfireNotConfiguredWarning during tests only
os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")
