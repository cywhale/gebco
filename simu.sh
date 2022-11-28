#!/bin/bash
#### most-performant, add worker, --threads, --worker-connections seems no extra benefits
# gunicorn read_gebco01:app -w 4 -k uvicorn.workers.UvicornWorker --reload
#### maximum no-error limit (if -p 3, always throw error)
# autocannon -c 100 -d 4 -p 2 -w 3 http://localhost:8000/
#### delete gunicorn proc
# ps -ef | grep 'gunicorn' | grep -v grep | awk '{print $2}' | xargs -r kill -9

# https
source "$HOME/python/py38/bin/activate"
cd "$HOME/python/gebco"
gunicorn read_gebco01:app -w 4 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8013 --keyfile conf/privkey.pem --certfile conf/fullchain.pem --reload
