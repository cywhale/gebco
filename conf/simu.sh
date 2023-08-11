#!/bin/bash
#### most-performant, add worker, --threads, --worker-connections seems no extra benefits
# gunicorn gebco_app:app -w 4 -k uvicorn.workers.UvicornWorker --reload
#### maximum no-error limit (if -p 3, always throw error)
# use simulation file
# gunicorn simu_gebco01:app -w 4 -k uvicorn.workers.UvicornWorker --reload
# autocannon -c 100 -d 4 -p 2 -w 3 http://localhost:8000/gebco
#### delete gunicorn proc
# ps -ef | grep 'gunicorn' | grep -v grep | awk '{print $2}' | xargs -r kill -9

#### statics: sum the distances
# gawk -F',' '{ sum += $1 } END{ print sum, NR }'  simu/test_dis.csv

#### others
#### requirements
# pipreqs --force ./

# https
source "$HOME/python/py38/bin/activate"
cd "$HOME/python/gebco"
gunicorn gebco_app:app -w 4 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8013 --keyfile conf/privkey.pem --certfile conf/fullchain.pem --reload

# debug
gunicorn gebco_app:app -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8013 --keyfile conf/privkey.pem --certfile conf/fullchain.pem --reload --capture-output --log-level debug --access-logfile - --error-logfile -

# pm2 start
pm2 start ./conf/ecosystem.config.js


#### pyenv to upgrade python version (if needed)
deactivate
cd ~/python
curl https://pyenv.run | bash
# ./bashrc
# echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
##### should append, not directly echo 'export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
# echo -e 'if command -v pyenv 1>/dev/null 2>&1; then\n eval "$(pyenv init -)"\nfi' >> ~/.bashrc
exec $SHELL #souce ~/.bashrc
pyenv update
pyenv versions
pyenv install 3.10:latest
pyenv local 3.10.10
python3.10 -m venv py310



