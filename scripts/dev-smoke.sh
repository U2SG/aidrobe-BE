#!/usr/bin/env bash
# End-to-end smoke on the local stack: presign -> upload to MinIO -> import ->
# confirm (top, bottom, shoes: the first-outfit minimum) -> GET /v1/recommendations/today.
# Uses the dev X-User-Id shortcut; every provider is fake, so it costs nothing.
set -euo pipefail
api="${API:-http://localhost:8000}"
user="${DEV_USER_ID:-$(uuidgen | tr 'A-Z' 'a-z')}"
hdr=(-H "X-User-Id: $user" -H "Content-Type: application/json")
img="$(mktemp -t aidrobe-smoke).jpg"
trap 'rm -f "$img"' EXIT
# 1x1 JPEG
printf '\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\x27 ",#\x1c\x1c(7),01444\x1f\x27=9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00T\xbf\xff\xd9' >"$img"

jqr() { python3 -c "import json,sys; d=json.load(sys.stdin); d=d.get('data', d) if isinstance(d, dict) and 'code' in d else d; print(eval(sys.argv[1], {'d': d}))" "$1"; }

import_item() { # $1 = name hint (top|pants), $2 = category, $3 = title
  local presign url fields file_url task status
  presign=$(curl -fsS "${hdr[@]}" -X POST "$api/v1/uploads/presign" \
    -d "{\"file_name\":\"$1.jpg\",\"content_type\":\"image/jpeg\"}")
  file_url=$(jqr "d['file_url']" <<<"$presign")
  # Same rewrite the apps do: the presign carries the in-cluster host (minio).
  url=$(python3 -c "import sys,urllib.parse as u; a=u.urlsplit(sys.argv[1]); b=u.urlsplit(sys.argv[2]); print(u.urlunsplit(a._replace(netloc=b.netloc, scheme=b.scheme)) if a.hostname in {'minio','localhost','127.0.0.1','0.0.0.0'} else sys.argv[1])" "$(jqr "d['upload_url']" <<<"$presign")" "$file_url")
  fields=$(jqr "' '.join('-F ' + k + '=' + v for k, v in (d.get('fields') or {}).items())" <<<"$presign")
  # shellcheck disable=SC2086
  curl -fsS -o /dev/null $fields -F "file=@$img;type=image/jpeg" "$url"
  task=$(curl -fsS "${hdr[@]}" -X POST "$api/v1/import-tasks" \
    -d "{\"input_type\":\"image\",\"source_type\":\"album\",\"image_url\":\"$file_url\"}" | jqr "d['task_id']")
  for _ in $(seq 1 60); do
    status=$(curl -fsS "${hdr[@]}" "$api/v1/import-tasks/$task" | jqr "d['status']")
    [[ "$status" == waiting_confirm ]] && break
    [[ "$status" == failed ]] && { echo "import $1 failed" >&2; exit 1; }
    sleep 2
  done
  [[ "$status" == waiting_confirm ]] || { echo "import $1 stuck in $status (is the worker running?)" >&2; exit 1; }
  curl -fsS "${hdr[@]}" -X POST "$api/v1/import-tasks/$task/confirm" \
    -d "{\"title\":\"$3\",\"category\":\"$2\",\"color_main\":\"white\"}" >/dev/null
  echo "imported $2 ($task)"
}

echo "user $user"
import_item top top "Smoke Tee"
import_item pants bottom "Smoke Trousers"
import_item shoes shoes "Smoke Loafers"

for _ in $(seq 1 30); do
  today=$(curl -fsS "${hdr[@]}" --max-time 60 "$api/v1/recommendations/today")
  count=$(jqr "len(d.get('outfits') or [])" <<<"$today")
  if [[ "$count" -gt 0 ]]; then
    echo "today: $count outfit(s)"
    exit 0
  fi
  sleep 2
done
echo "today stayed empty after import" >&2
exit 1
