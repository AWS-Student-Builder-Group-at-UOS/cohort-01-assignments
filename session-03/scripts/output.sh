#!/bin/sh
# 출력값을 못 찾으면 다음 AWS 명령을 빈 이름으로 실행하지 않고 중단한다.
set -eu
stack=$1
region=$2
key=$3

value=$(aws cloudformation describe-stacks --stack-name "$stack" --region "$region" \
  --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text)
case "$value" in
  ''|None|null)
    printf '스택 %s에 %s 출력이 없습니다. 배포 단계와 REGION을 확인하세요.\n' "$stack" "$key" >&2
    exit 1
    ;;
esac
printf '%s\n' "$value"
