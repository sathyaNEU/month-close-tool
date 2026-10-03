# Local toolchain for the close platform (see tools/versions.lock for the pinned versions).
# `make bootstrap` runs `brew bundle` with --no-upgrade, so a second run changes nothing.
brew "uv"
brew "openjdk@21"
brew "jq"
# The Databricks CLI (databricks/tap/databricks) is added in Stage 10. Homebrew 7 requires
# an explicit `brew trust` for third-party taps, which needs the user's go-ahead.
