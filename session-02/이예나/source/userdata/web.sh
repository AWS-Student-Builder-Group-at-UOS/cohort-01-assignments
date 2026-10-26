#!/bin/bash
set -euo pipefail

# 서울 리전에 생성한 실습 리소스.
BUCKET=i2na-asbg-artifacts
APP_ALB=internal-i2na-asbg-alb-internal-323721922.ap-northeast-2.elb.amazonaws.com

dnf install -y nginx
mkdir -p /srv/portal
aws s3 cp "s3://$BUCKET/web.tar.gz" /tmp/web.tar.gz --region ap-northeast-2
tar -xzf /tmp/web.tar.gz -C /srv/portal --no-same-owner

TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
meta() { curl -sH "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/$1"; }
WEB_INSTANCE="$(meta instance-id) $(meta placement/availability-zone)"

sed -e "s|__APP_ALB__|$APP_ALB|" -e "s|__WEB_INSTANCE__|$WEB_INSTANCE|" \
  /srv/portal/web/nginx.conf > /etc/nginx/nginx.conf
systemctl enable --now nginx
