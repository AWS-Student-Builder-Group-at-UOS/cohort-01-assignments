#!/bin/bash
set -euo pipefail

# 서울 리전에 생성한 실습 리소스.
BUCKET=i2na-asbg-artifacts
DB_HOST=i2na-asbg-db.cp4o44q6ucys.ap-northeast-2.rds.amazonaws.com
DB_SECRET_ID='arn:aws:secretsmanager:ap-northeast-2:888195519399:secret:rds!db-6adacff3-d175-4485-904c-4dbb084834fa-tvLg0Q'
REDIS_URL=rediss://i2na-asbg-cache-aal1rq.serverless.apn2.cache.amazonaws.com:6379

dnf install -y nodejs24
mkdir -p /srv/portal
aws s3 cp "s3://$BUCKET/app.tar.gz" /tmp/app.tar.gz --region ap-northeast-2
tar -xzf /tmp/app.tar.gz -C /srv/portal --no-same-owner

cat > /etc/portal-app.env <<ENV
AWS_REGION=ap-northeast-2
DB_HOST=$DB_HOST
DB_SECRET_ID=$DB_SECRET_ID
SESSION_SECRET_ID=i2na-asbg/session-secret
REDIS_URL=$REDIS_URL
ENV

cp /srv/portal/app/portal-app.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now portal-app
