# Find a Python 3.11 or later for the launchers in this folder.
#
# Sourced by each launcher after it has set JEV_PLUGIN_ROOT from its own
# location; it is never run on its own and never read from the working
# folder. jev_find_python sets JEV_PYTHON, or prints everything it tried to
# stderr and returns 3.
#
# Order: the bundled interpreter; the usual system locations; an absolute
# JEV_PYTHON you set; python3.14 ... python3.11 and python3 on PATH; then
# ~/.pyenv/shims/python3 and ~/.local/bin/python3. A PATH entry is used only
# if it is absolute and outside the folder the host opened: ".", an empty
# entry, or a folder inside a cloned repository would let that repository
# choose the interpreter.

jev_requested_python=${JEV_PYTHON-}
JEV_PYTHON=''
jev_tried=''
jev_seen='|'
jev_here_logical=${PWD-}
jev_here_physical=$(pwd -P 2>/dev/null) || jev_here_physical=''
# The home folder is not a cloned repository, and Python managers keep their
# interpreters under it: a host started in ~ still finds them.
jev_home_physical=$(CDPATH= cd -- "${HOME:-/nonexistent}" 2>/dev/null && pwd -P) || jev_home_physical=''

jev_inside_working_folder() {
  jev_real=$(CDPATH= cd -- "$1" 2>/dev/null && pwd -P) || jev_real=$1
  for jev_here in "$jev_here_logical" "$jev_here_physical"; do
    case "$jev_here" in
      ''|/|"${HOME-}"|"$jev_home_physical") continue ;;
    esac
    case "$1" in
      "$jev_here"|"$jev_here"/*) return 0 ;;
    esac
    case "$jev_real" in
      "$jev_here"|"$jev_here"/*) return 0 ;;
    esac
  done
  return 1
}

jev_note() {
  jev_tried="${jev_tried:+$jev_tried, }$1"
}

jev_try() {
  case "$jev_seen" in
    *"|$1|"*) return 1 ;;
  esac
  jev_seen="$jev_seen$1|"
  if [ ! -f "$1" ] || [ ! -x "$1" ]; then
    jev_note "$1 (not found)"
    return 1
  fi
  if "$1" -I -S -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 3)' >/dev/null 2>&1; then
    JEV_PYTHON=$1
    return 0
  fi
  jev_note "$1 (older than 3.11, or does not run)"
  return 1
}

jev_try_on_path() {
  jev_rest=${PATH-}
  jev_found=''
  while [ -n "$jev_rest" ]; do
    case "$jev_rest" in
      *:*) jev_dir=${jev_rest%%:*}; jev_rest=${jev_rest#*:} ;;
      *) jev_dir=$jev_rest; jev_rest='' ;;
    esac
    case "$jev_dir" in
      /*)
        if [ -f "$jev_dir/$1" ] && [ -x "$jev_dir/$1" ]; then
          jev_found=1
          if jev_inside_working_folder "$jev_dir"; then
            jev_note "$jev_dir/$1 (inside the opened folder, not used)"
          else
            jev_try "$jev_dir/$1" && return 0
          fi
        fi
        ;;
    esac
  done
  [ -n "$jev_found" ] || jev_note "$1 (not on PATH)"
  return 1
}

jev_find_python() {
  jev_try "$JEV_PLUGIN_ROOT/runtime/python/bin/python3" && return 0
  for jev_fixed in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    jev_try "$jev_fixed" && return 0
  done
  case "$jev_requested_python" in
    '') jev_note 'JEV_PYTHON (not set)' ;;
    /*) jev_try "$jev_requested_python" && return 0 ;;
    *) jev_note 'JEV_PYTHON (not an absolute path)' ;;
  esac
  for jev_name in python3.14 python3.13 python3.12 python3.11 python3; do
    jev_try_on_path "$jev_name" && return 0
  done
  case "${HOME-}" in
    /*)
      jev_try "$HOME/.pyenv/shims/python3" && return 0
      jev_try "$HOME/.local/bin/python3" && return 0
      ;;
    *) jev_note 'HOME (not set to an absolute path)' ;;
  esac
  printf '%s\n' 'PYTHON_3_11_REQUIRED' "Looked for Python 3.11 or later: $jev_tried." \
    'Install Python 3.11 or later, or set JEV_PYTHON to the absolute path of one.' >&2
  return 3
}
