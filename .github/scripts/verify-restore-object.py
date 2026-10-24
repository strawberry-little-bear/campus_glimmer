"""对象镜像恢复后按原 key 逐字节比对，确认媒体文件也真的回来了。"""

import os

import boto3
from botocore.config import Config

BUCKET = 'campus-glimmer-restore'
KEY = 'media/ci-restore-proof.txt'
EXPECTED = b'campus glimmer ci restore proof\n'


def object_client():
    return boto3.session.Session().client(
        's3',
        endpoint_url=os.environ['DJANGO_S3_ENDPOINT_URL'],
        region_name=os.environ.get('DJANGO_S3_REGION', 'us-east-1'),
        aws_access_key_id=os.environ['DJANGO_S3_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['DJANGO_S3_SECRET_ACCESS_KEY'],
        config=Config(
            signature_version=os.environ.get(
                'DJANGO_S3_SIGNATURE_VERSION', 's3v4'),
            s3={'addressing_style': os.environ.get(
                'DJANGO_S3_ADDRESSING_STYLE', 'path')},
        ),
    )


def main():
    body = object_client().get_object(Bucket=BUCKET, Key=KEY)['Body'].read()
    if body != EXPECTED:
        raise SystemExit('%s/%s 字节内容与备份前不一致' % (BUCKET, KEY))
    print('按原 key 恢复成功：%s/%s，%d 字节' % (BUCKET, KEY, len(body)))


main()
