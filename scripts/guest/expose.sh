#!/vmf/sh
# vmf expose daemon (guest side). Discovers listeners every 2s and:
#   compose mode : socat per declared (ports.txt) +, with expose=all,
#                  EXPOSEd container port -> container IP (TCP+UDP)
#   direct mode  : records VM-local listeners only; the host poller adds
#                  slirp hostfwd entries that land on them directly
# Writes /vmf/ports-live.txt ("proto guest_port target ...") which the
# host-side poller (scripts/expose-poller.sh) turns into slirp hostfwd.
set -u
BB=/vmf/busybox
mode=$($BB cat /vmf-run/mode 2>/dev/null || echo direct)
expose=$($BB cat /vmf-run/expose 2>/dev/null || echo all)
[ "$expose" != "none" ] || exit 0
SOCAT=/data/docker/bin/socat
pids=/vmf/expose.pids
live=/vmf/ports-live.txt
desired=/vmf/expose.desired
: > "$pids"

while :; do
  : > "$desired.tmp"
  if [ "$mode" = "compose" ] && [ -x "$SOCAT" ] && [ -f /data/ports.txt ]; then
    PATH=/data/docker/bin:$PATH
    # Declared forwards: "proto vm_port container_port service".
    while read -r proto hport cport svc; do
      [ -n "${proto:-}" ] || continue
      cid=$(docker ps -q --filter "label=com.docker.compose.service=$svc" | $BB head -1)
      [ -n "$cid" ] || continue
      cip=$(docker inspect "$cid" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)
      [ -n "$cip" ] || continue
      $BB echo "$proto $hport $cip $cport $svc" >> "$desired.tmp"
    done < /data/ports.txt
    # Auto mode: every other EXPOSEd container port, forwarded 1:1.
    if [ "$expose" = "all" ]; then
      for cid in $(docker ps -q); do
        svc=$(docker inspect "$cid" --format '{{index .Config.Labels "com.docker.compose.service"}}' 2>/dev/null)
        cip=$(docker inspect "$cid" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' 2>/dev/null)
        [ -n "$cip" ] || continue
        for pp in $(docker inspect "$cid" --format '{{range $p, $_ := .Config.ExposedPorts}}{{$p}} {{end}}' 2>/dev/null); do
          cport=${pp%/*}; proto=${pp#*/}
          [ "$proto" = "tcp" ] || [ "$proto" = "udp" ] || continue
          $BB grep -qE "^[a-z]+ [0-9]+ [0-9.]+ $cport " "$desired.tmp" && continue
          $BB echo "$proto $cport $cip $cport ${svc:-container}" >> "$desired.tmp"
        done
      done
    fi
  elif [ "$mode" != "compose" ]; then
    # Direct mode: VM-local listeners. slirp hostfwd reaches 0.0.0.0 and
    # 10.0.2.15 (0F02000A little-endian) only; skip sshd + ephemeral.
    # expose=all records every listener; expose=declared keeps only the
    # published (hostfwd) ports — the reconcile then bridges the
    # loopback-bound ones to the VM address.
    # NOTE: explicit per-loop redirects — a `{ ... } >> file` group
    # around these loops emitted nothing under busybox ash.
    for f in /proc/net/tcp /proc/net/tcp6; do
      [ -r "$f" ] || continue
      $BB awk 'function h2d(h,   n, i) {
        n = 0
        for (i = 1; i <= length(h); i++)
          n = n * 16 + index("0123456789abcdef", tolower(substr(h, i, 1))) - 1
        return n
      }
      $4 == "0A" {
        split($2, a, ":"); port = h2d(a[2]);
        if (port >= 32768 && port <= 60999) next;
        if (port == 22) next;
        # VM-address binds (00000000/0F02000A) reach hostfwd directly;
        # loopback binds (0100007F) need the socat bridge below.
        if (length(a[1]) == 8 && a[1] != "00000000" && a[1] != "0F02000A" \
            && a[1] != "0100007F") next;
        if (length(a[1]) == 32 && a[1] != "00000000000000000000000000000000" \
            && a[1] != "00000000000000000000000000000001") next;
        print "tcp " port " vm - -"
      }' "$f" >> "$desired.tmp"
    done
    for f in /proc/net/udp /proc/net/udp6; do
      [ -r "$f" ] || continue
      $BB awk 'function h2d(h,   n, i) {
        n = 0
        for (i = 1; i <= length(h); i++)
          n = n * 16 + index("0123456789abcdef", tolower(substr(h, i, 1))) - 1
        return n
      }
      {
        split($2, a, ":"); port = h2d(a[2]);
        if (port == 0) next;
        if (port >= 32768 && port <= 60999) next;
        if (port == 67 || port == 68) next;
        if (length(a[1]) == 8 && a[1] != "00000000" && a[1] != "0F02000A" \
            && a[1] != "0100007F") next;
        if (length(a[1]) == 32 && a[1] != "00000000000000000000000000000000" \
            && a[1] != "00000000000000000000000000000001") next;
        print "udp " port " vm - -"
      }' "$f" >> "$desired.tmp"
    done
    # declared: keep only published ports (the bridge reconcile reads
    # the same list; the firecracker poller must not publish extras).
    if [ "$expose" = "declared" ] && [ -r /vmf-run/hostfwd ]; then
      $BB mv "$desired.tmp" "$desired.all"
      : > "$desired.tmp"
      while read -r fproto fhp fgp; do
        [ -n "${fproto:-}" ] || continue
        $BB grep -qE "^$fproto $fgp vm( |$)" "$desired.all" && \
          $BB echo "$fproto $fgp vm - -" >> "$desired.tmp"
      done < /vmf-run/hostfwd
      $BB rm -f "$desired.all"
    fi
  fi
  sort -u "$desired.tmp" > "$desired.new" 2>/dev/null
  mv "$desired.new" "$desired"
  # Reconcile socat forwards: first service to claim a (proto, port)
  # keeps it; late claims on a taken port are conflicts.
  : > "$pids.new"
  while read -r proto lport cip cport svc; do
    [ -n "${proto:-}" ] || continue
    if [ "${cip:-}" = "vm" ]; then
      # Loopback-bound listener: slirp hostfwd targets the VM address,
      # not 127.0.0.1, so a loopback-only app is unreachable through a
      # published port. Bridge DECLARED ports to the loopback listener
      # (the same socat pattern compose mode uses for container IPs).
      # sshd and ephemeral ports never reach this list.
      key="$proto:$lport"
      old=""
      [ -f "$pids" ] && old=$($BB grep -E "^$key " "$pids" | $BB head -1)
      oldpid=""
      [ -n "$old" ] && oldpid=$($BB echo "$old" | cut -d' ' -f2)
      if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
        $BB echo "$old" >> "$pids.new"
        continue
      fi
      declared=0
      if [ -r /vmf-run/hostfwd ]; then
        while read -r fproto fhp fgp; do
          [ "$fproto" = "$proto" ] && [ "$fgp" = "$lport" ] && declared=1
        done < /vmf-run/hostfwd
      fi
      if [ "$declared" = "1" ] && [ -x /vmf/socat ]; then
        if [ "$proto" = "udp" ]; then
          /vmf/socat -T60 "UDP4-RECVFROM:$lport,bind=10.0.2.15,fork,reuseaddr" "UDP4-SENDTO:127.0.0.1:$lport" >/dev/null 2>&1 &
        else
          /vmf/socat "TCP4-LISTEN:$lport,bind=10.0.2.15,fork,reuseaddr" "TCP4:127.0.0.1:$lport" >/dev/null 2>&1 &
        fi
        $BB echo "$key $! 127.0.0.1 $svc" >> "$pids.new"
        $BB echo "vmf-expose: loopback bridge $proto $lport (declared)"
      fi
      continue
    fi
    [ -n "$cip" ] || continue
    key="$proto:$lport"
    old=""
    [ -f "$pids" ] && old=$($BB grep -E "^$key " "$pids" | $BB head -1)
    oldpid=""; oldcip=""; oldsvc=""
    if [ -n "$old" ]; then
      oldpid=$($BB echo "$old" | cut -d' ' -f2)
      oldcip=$($BB echo "$old" | cut -d' ' -f3)
      oldsvc=$($BB echo "$old" | cut -d' ' -f4)
    fi
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null && [ "$oldsvc" != "$svc" ]; then
      $BB echo "vmf-expose: port conflict $proto/$lport: keeping $oldsvc, skipping $svc"
      $BB echo "$key $oldpid $oldcip $oldsvc" >> "$pids.new"
      continue
    fi
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null && [ "$oldcip" = "$cip" ]; then
      $BB echo "$key $oldpid $cip $svc" >> "$pids.new"
      continue
    fi
    [ -n "$oldpid" ] && kill "$oldpid" 2>/dev/null
    if [ "$proto" = "udp" ]; then
      "$SOCAT" -T60 "UDP4-RECVFROM:$lport,fork,reuseaddr" "UDP4-SENDTO:$cip:$cport" >/dev/null 2>&1 &
    else
      "$SOCAT" "TCP4-LISTEN:$lport,fork,reuseaddr" "TCP4:$cip:$cport" >/dev/null 2>&1 &
    fi
    $BB echo "$key $! $cip $svc" >> "$pids.new"
    $BB echo "vmf-expose: $proto $lport -> $cip:$cport ($svc)"
  done < "$desired"
  mv "$pids.new" "$pids"
  # Port table the host poller turns into slirp hostfwd.
  : > "$live.tmp"
  while read -r proto lport cip cport svc; do
    [ -n "${proto:-}" ] || continue
    if [ "${cip:-}" = "vm" ]; then
      $BB echo "$proto $lport vm" >> "$live.tmp"
    elif [ -n "$cip" ]; then
      $BB echo "$proto $lport $cip:$cport $svc" >> "$live.tmp"
    fi
  done < "$desired"
  sort -u "$live.tmp" > "$live.new" 2>/dev/null
  mv "$live.new" "$live"
  $BB sleep 2
done
