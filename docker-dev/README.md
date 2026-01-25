### Connect to docker

Local config in `.zshrc`:

```zsh
function docker_exec() {
	local container_name="${2:-lsyin_sgl}"
	local remote_cmd="docker exec -it ${container_name} zsh"
	ssh -t "$1" "$remote_cmd"
}
```

Example usage:

```zsh
docker_exec hyper
```
