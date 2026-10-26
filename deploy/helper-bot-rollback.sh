#!/usr/bin/env bash
set -Eeuo pipefail

app_root=/opt/helper-bot
current="$app_root/current"
requested="${1:-}"

if [[ -n "$requested" ]]; then
  [[ "$requested" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid release id" >&2; exit 2; }
  target="$app_root/releases/$requested"
else
  current_target="$(readlink -f "$current")"
  target="$(find "$app_root/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2- | grep -Fvx "$current_target" | head -n 1)"
fi

[[ -n "$target" && -d "$target" ]] || { echo "release not found" >&2; exit 2; }
ln -sfn "$target" "$app_root/.current-next"
mv -Tf "$app_root/.current-next" "$current"
systemctl restart helper-bot
curl --fail --retry 10 --retry-delay 2 --retry-connrefused http://127.0.0.1:8080/healthz
