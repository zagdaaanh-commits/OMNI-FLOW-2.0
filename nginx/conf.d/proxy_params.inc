# Shared proxy settings (included from app.conf; the .inc suffix keeps nginx from loading it standalone).
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

# WebSocket pass-through
proxy_set_header Upgrade    $http_upgrade;
proxy_set_header Connection $connection_upgrade;

# LLM and image-upload endpoints can take a while.
proxy_connect_timeout 5s;
proxy_send_timeout    120s;
proxy_read_timeout    120s;
proxy_buffering       on;

# During rolling restarts a request that hit a stopping replica is retried on another one,
# but only idempotent methods (nginx never retries POST by default).
proxy_next_upstream         error timeout http_502 http_503 http_504;
proxy_next_upstream_tries   3;
proxy_next_upstream_timeout 10s;
