#!/usr/bin/env bash
set -Eeuo pipefail

release_id="${1:-}"
archive="${2:-}"
app_root=/opt/helper-bot
releases="$app_root/releases"
current="$app_root/current"

if [[ ! "$release_id" =~ ^[0-9a-f]{40}$ ]]; then
  echo "release id must be a full Git SHA" >&2
  exit 2
fi
if [[ "$archive" != "/tmp/helper-bot-${release_id}.tar.gz" || ! -f "$archive" ]]; then
  echo "unexpected or missing release archive" >&2
  exit 2
fi

install -d -o helperbot -g helperbot "$releases"
target="$releases/$release_id"
previous=""
if [[ -L "$current" ]]; then
  previous="$(readlink -f "$current")"
fi
if [[ -e "$target" ]]; then
  echo "release already exists: $target" >&2
  exit 2
fi

install -d -o helperbot -g helperbot "$target"
tar -xzf "$archive" -C "$target"
rm -f "$archive"
chown -R helperbot:helperbot "$target"

runuser -u helperbot -- python3.12 -m venv "$target/.venv"
runuser -u helperbot -- "$target/.venv/bin/python" -m pip install --disable-pip-version-check -r "$target/requirements.txt"
runuser -u helperbot -- "$target/.venv/bin/python" -m compileall -q \
  "$target/bot.py" "$target/db.py" "$target/firebase_db.py" "$target/gpt.py" \
  "$target/matching.py" "$target/tests" "$target/tools"
runuser -u helperbot -- "$target/.venv/bin/python" -m unittest discover -s "$target/tests" -v

ln -sfn "$target" "$app_root/.current-next"
mv -Tf "$app_root/.current-next" "$current"
systemctl restart helper-bot

healthy=false
for _ in {1..20}; do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8080/healthz >/dev/null; then
    healthy=true
    break
  fi
  sleep 2
done

if [[ "$healthy" != true ]]; then
  echo "health check failed; restoring previous release" >&2
  if [[ -n "$previous" && -d "$previous" ]]; then
    ln -sfn "$previous" "$app_root/.current-next"
    mv -Tf "$app_root/.current-next" "$current"
    systemctl restart helper-bot
  fi
  exit 1
fi

# Keep the current release and four predecessors.
mapfile -t old_releases < <(find "$releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | tail -n +6 | cut -d' ' -f2-)
for old_release in "${old_releases[@]}"; do
  [[ "$old_release" == "$(readlink -f "$current")" ]] || rm -rf -- "$old_release"
done
