#!/bin/sh
set -eu
region=$1
python=$2
missing=0
for tool in aws sam make "$python"; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf '설치 필요: %s\n' "$tool" >&2
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  printf '%s\n' 'README의 '준비물과 명령'을 확인하거나 AWS CloudShell에서 실행하세요.' >&2
  exit 1
fi
aws --version
sam --version
"$python" -c 'import sys; print("로컬 Python:", sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 9) else "로컬 테스트에는 Python 3.9 이상이 필요합니다.")'
printf '%s\n' 'Lambda 런타임: Python 3.12 (로컬 3.12 설치·Docker 불필요)'
aws sts get-caller-identity --region "$region" --query '{Account:Account,Arn:Arn}' --output table
printf '준비 완료 · 리전 %s · 위 AWS 계정에 배포됩니다.\n' "$region"
