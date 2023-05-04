module.exports = {
  apps : [{
    name: 'gebco',
    script: 'gunicorn gebco_app:app -w 4 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8013 --keyfile conf/privkey.pem --certfile conf/fullchain.pem --reload',
    args: '',
    merge_logs: true,
    autorestart: true,
    log_file: "tmp/combined.outerr.log",
    out_file: "tmp/out.log",
    error_file: "tmp/err.log",
    log_date_format : "YYYY-MM-DD HH:mm Z",
    append_env_to_name: true,
    watch: false,
    max_memory_restart: '4G',
  }],
};

