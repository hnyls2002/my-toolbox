syntax on

set cindent
set number
set autoindent
set smartindent
set tabstop=4
set softtabstop=4
set shiftwidth=4
set backspace=2
set mouse=a
set cursorline
set cursorcolumn
set hlsearch
set wrap

" Make the search case insensitive
set ignorecase
" But make them case-sensitive if they include capitals
set smartcase

" disable fold opening by jump
set foldopen=

map <C-h> 5h
map <C-j> 5j
map <C-k> 5k
map <C-l> 5l
imap jk <Esc>

map <C-a> ggVG
imap <C-a> ggVG

let g:powerline_loaded=1
