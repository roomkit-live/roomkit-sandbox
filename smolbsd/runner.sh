#!/bin/sh
#
# roomkit-sandbox runner: runs inside the smolBSD guest and executes the
# commands SmolBSDSandboxBackend sends over a VirtIO console port.
#
# One line per message in each direction, binary fields base64-encoded
# on a single line (an empty field is an empty string):
#
#   PING <id>                          -> PONG <id>
#   ENV <id> <b64 sh script>           -> OK <id>
#   EXEC <id> <timeout> <b64 script>   -> RES <id> <rc> <b64 stdout> <b64 stderr>
#   any request that fails             -> ERR <id> <reason>
#
# ENV stores the session environment, sourced before every EXEC. A
# command still running after <timeout> seconds is killed with its
# children and reports exit code 124. Output beyond RUNNER_OUTPUT_LIMIT
# bytes per stream is cut, with a notice appended to stderr.

PORT=${RUNNER_PORT:-/dev/ttyVI02}
ENVFILE=${RUNNER_ENVFILE:-/var/roomkit/env}
LIMIT=${RUNNER_OUTPUT_LIMIT:-4194304}

b64()
{
	base64 | tr -d '\n'
}

# Parent first: a shell whose child dies runs its next line (the
# watchdog would mark a timeout, a script would go on), so it must be
# gone before its children are. The child list is read beforehand.
killtree()
{
	children=$(pgrep -P "$1")
	kill -KILL "$1" 2>/dev/null
	for child in $children; do
		killtree "$child"
	done
}

do_env()
{
	if (
		umask 077
		mkdir -p "${ENVFILE%/*}" &&
			printf '%s' "$2" | base64 -d >"$ENVFILE"
	); then
		printf 'OK %s\n' "$1"
	else
		printf 'ERR %s cannot store the environment\n' "$1"
	fi
}

# note_cut <stream> <file>: explain on stderr why a stream came back short
note_cut()
{
	size=$(wc -c <"$2")
	[ "$size" -gt "$LIMIT" ] && \
		printf '\n[roomkit-runner] %s cut to %s of %s bytes\n' \
			"$1" "$LIMIT" "$size"
}

do_exec()
{
	id=$1
	timeout=$2
	if ! work=$(mktemp -d /tmp/roomkit.XXXXXX); then
		printf 'ERR %s cannot create a work directory\n' "$id"
		return
	fi
	printf '%s' "$3" | base64 -d >"$work/script"

	(
		[ -f "$ENVFILE" ] && . "$ENVFILE"
		exec sh "$work/script"
	) </dev/null >"$work/out" 2>"$work/err" &
	pid=$!
	( sleep "$timeout"; touch "$work/timeout"; killtree "$pid" ) &
	watchdog=$!
	wait "$pid"
	rc=$?
	if [ -f "$work/timeout" ]; then
		# the watchdog fired: let it finish killing the command's tree
		wait "$watchdog"
		rc=124
	else
		killtree "$watchdog"
	fi
	# what a finished command wrote survives the VM being killed
	sync

	{ note_cut stdout "$work/out"; note_cut stderr "$work/err"; } >"$work/notice"
	printf 'RES %s %s ' "$id" "$rc"
	head -c "$LIMIT" "$work/out" | b64
	printf ' '
	{ head -c "$LIMIT" "$work/err"; cat "$work/notice"; } | b64
	printf '\n'
	rm -rf "$work"
}

serve()
{
	exec <"$PORT" >"$PORT"
	# raw and no echo, or the host reads its own requests back with CRLFs
	stty raw -echo
	while IFS= read -r line; do
		set -f
		# shellcheck disable=SC2086 # split the request into its fields
		set -- $line
		set +f
		case $1 in
		PING) printf 'PONG %s\n' "$2" ;;
		ENV) do_env "$2" "$3" ;;
		EXEC) do_exec "$2" "$3" "$4" ;;
		*) printf 'ERR %s unknown request\n' "${2:-0}" ;;
		esac
	done
}

# the port is reopened if it ever reads EOF, so the VM stays usable
while :; do
	(serve)
	sleep 1
done
