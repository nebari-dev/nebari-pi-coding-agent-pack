#!/usr/bin/env bash
# Internal link checker for the Astro/Starlight docs site.
# Verifies every internal href/src in docs/dist/**/*.html resolves to an
# existing file under dist/. Exits 0 on success, 1 with offenders on failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$REPO_ROOT/docs/dist"

if [ -n "${SKIP_BUILD:-}" ]; then
    [ -d "$DIST_DIR" ] || { echo "ERROR: SKIP_BUILD set but $DIST_DIR missing." >&2; exit 1; }
    echo "SKIP_BUILD set - checking existing $DIST_DIR."
else
    echo "Building site..."
    (cd "$REPO_ROOT/docs" && npm run build)
fi

BASE="${BASE:-/}"
SUBPATH_PREFIX="$(printf '%s' "$BASE" | sed 's:/*$::')"

resolve_path() {
    local href="$1"
    if [ -n "$SUBPATH_PREFIX" ]; then
        href="${href#"$SUBPATH_PREFIX"}"
    fi
    [ -z "$href" ] && href="/"
    if [[ "$href" == */ ]]; then
        echo "$DIST_DIR${href}index.html"
    else
        echo "$DIST_DIR$href"
    fi
}

BROKEN_LINKS=()
while IFS= read -r -d '' html_file; do
    while IFS= read -r url; do
        [ -z "$url" ] && continue
        case "$url" in
            http://*|https://*|//*) continue ;;
            \#*|mailto:*|data:*) continue ;;
            /*) ;;
            *) continue ;;
        esac
        url="${url%%#*}"
        [ -z "$url" ] && continue
        target="$(resolve_path "$url")"
        [ -e "$target" ] || BROKEN_LINKS+=("BROKEN: $html_file -> $url (resolved: $target)")
    done < <(
        grep -oE 'href="[^"]*"|src="[^"]*"' "$html_file" \
            | sed -E 's/^(href|src)="//; s/"$//'
    )
done < <(find "$DIST_DIR" -name "*.html" -print0)

if [ ${#BROKEN_LINKS[@]} -gt 0 ]; then
    echo ""
    echo "Internal link failures (${#BROKEN_LINKS[@]}):"
    printf '  %s\n' "${BROKEN_LINKS[@]}"
    exit 1
fi
echo "LINKS_OK"
