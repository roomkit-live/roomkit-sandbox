#!/bin/sh
#
# roomkit-sandbox workspace: mount the VM's workspace disk on /workspace,
# formatting it on first boot.
#
# The backend attaches one blank sparse disk per VM, kept until the VM is
# deleted. Device numbering is not stable (the root disk can be ld1), so
# disks are told apart by content: the formatted workspace carries a GPT
# wedge labelled $LABEL; a disk is only formatted when it has no wedge at
# all (the root disk always has one) and its first 128 KiB are zero.

LABEL=roomkit-workspace
MNT=/workspace

mount_workspace()
{
	mount -o log "NAME=$LABEL" "$MNT" 2>/dev/null
}

blank_disk()
{
	for disk in $(sysctl -n hw.disknames); do
		case $disk in
		ld*) ;;
		*) continue ;;
		esac
		dkctl "$disk" listwedges 2>/dev/null | grep -q '^dk' && continue
		nonzero=$(dd if="/dev/r$disk" bs=64k count=2 2>/dev/null |
			od -An -v | tr -d ' 0\n' | wc -c)
		if [ "$nonzero" -eq 0 ]; then
			echo "$disk"
			return
		fi
	done
}

mount | grep -q " on $MNT " && exit 0
mount_workspace && exit 0

disk=$(blank_disk)
if [ -z "$disk" ]; then
	echo "roomkit-workspace: no workspace disk to mount or format" >&2
	exit 1
fi
if ! { gpt create "$disk" && gpt add -t ffs -l "$LABEL" "$disk" &&
	newfs -O 2 "NAME=$LABEL" && mount_workspace; } >/dev/null; then
	echo "roomkit-workspace: cannot format $disk" >&2
	exit 1
fi
