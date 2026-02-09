function! Yank(text) abort
  let encoded = system('base64 | tr -d "\n"', a:text)
  let osc52 = "\x1b]52;c;" . encoded . "\x07"
  if has('nvim')
    call system("echo -n " . shellescape(osc52) . " >> /proc/$PPID/fd/1")
  else
    call system('printf "%s" ' . shellescape(osc52) . ' > /dev/tty')
  endif
endfunction

autocmd TextYankPost * call Yank(join(v:event.regcontents, "\n"))
