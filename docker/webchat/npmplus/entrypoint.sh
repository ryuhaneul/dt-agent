#!/bin/sh
set -eu

# The image startup scripts edit nginx.conf (for example, the IPv6 resolver).
# Copy the verified policy first; never start with the image's default trust list.
cp /etc/dtw-nginx.conf /usr/local/nginx/conf/nginx.conf
exec tini -- entrypoint.sh "$@"
