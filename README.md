# Overcooked Demo
<p align="center">
<img src="./server/static/images/browser_view.png" >
</p>

A web application where humans can play Overcooked with trained AI agents.

## Stable Docker build

Check docker image "overcook:stable" and run
command
```bash
docker run -p 80:5000 -dit --name overcook --mount type=tmpfs,dst=/dev/shm --mount type=bind,src=/home/xxx/overcooked-demo/overcooked-demo/server,dst=/app overcook:stable
```
Replace the "src" mount directory with your code directory. Then go https:localhost to build the game.