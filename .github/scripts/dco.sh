#!/usr/bin/env bash
# DCO check of the commits of a pull request.
# Usage: dco.sh CODEOWNERS < commits
#   commits: the commit objects of GET /repos/{owner}/{repo}/pulls/{n}/commits, one JSON object per line.
# Exempt: a commit whose GitHub author login (or author email) is a user in CODEOWNERS, a commit by a bot
# (login ending in [bot]) and a merge commit. Every other commit needs "Signed-off-by: Name <email>".
# Exits 1 and names each commit without one.
set -euo pipefail

# Users only: "@login" or "name@example.com". Teams ("@org/team") are not users and exempt nobody.
owners=$(sed -e 's/#.*//' "$1" | tr -s ' \t' '\n\n' | grep -E '^(@[A-Za-z0-9-]+|[^@/[:space:]]+@[^@/[:space:]]+)$' \
  | sed 's/^@//' | tr '[:upper:]' '[:lower:]' | sort -u | jq -R . | jq -sc .)

jq -r --argjson owners "$owners" '
  select((.parents | length) < 2)
  | (.author.login // "") as $login
  | ((.commit.author.email // "") | ascii_downcase) as $email
  | select(($owners | index($login | ascii_downcase)) == null)
  | select(($owners | index($email)) == null)
  | select($login | endswith("[bot]") | not)
  | select(.commit.message | test("(?m)^Signed-off-by: [^<\n]+ <[^>\n]+@[^>\n]+>[ \t]*$") | not)
  | "\(.sha[0:12])\t\(if $login == "" then "unknown" else $login end)"' | {
  bad=0
  while IFS=$'\t' read -r sha login; do
    echo "::error::commit $sha (author $login) has no Signed-off-by line; sign it with git commit -s"
    bad=1
  done
  exit "$bad"
}
