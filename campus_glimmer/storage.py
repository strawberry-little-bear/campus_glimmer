"""S3 storage with separate internal and browser-facing endpoints.

Containers usually reach MinIO at ``http://minio:9000`` while browsers need
``http://localhost:9000``. Uploading through the internal endpoint and signing
download URLs against the browser endpoint keeps both paths valid without
making the bucket public.
"""

from urllib.parse import urlencode, urlparse

import boto3
from botocore.config import Config
from storages.backends.s3 import S3Storage
from storages.utils import clean_name


class PublicEndpointS3Storage(S3Storage):
    """Sign object URLs against a separately configured public endpoint."""

    def __init__(self, **settings):
        self.public_endpoint_url = settings.pop('public_endpoint_url', None)
        self._public_client = None
        super().__init__(**settings)

    def _public_boto_client(self):
        if self._public_client is None:
            self._public_client = boto3.session.Session().client(
                's3',
                endpoint_url=self.public_endpoint_url,
                region_name=self.region_name,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                aws_session_token=self.security_token,
                config=Config(
                    signature_version=self.signature_version,
                    s3={'addressing_style': self.addressing_style},
                ),
            )
        return self._public_client

    def _public_object_url(self, name):
        endpoint = self.public_endpoint_url.rstrip('/')
        if self.addressing_style == 'virtual':
            parsed = urlparse(endpoint)
            endpoint = (
                f'{parsed.scheme}://{self.bucket_name}.{parsed.netloc}'
                f'{parsed.path.rstrip("/")}'
            )
            return f'{endpoint}/{name}'
        return f'{endpoint}/{self.bucket_name}/{name}'

    def url(self, name, parameters=None, expire=None, http_method=None):
        if not self.public_endpoint_url or self.custom_domain:
            return super().url(
                name,
                parameters=parameters,
                expire=expire,
                http_method=http_method,
            )

        name = self._normalize_name(clean_name(name))
        if not self.querystring_auth:
            url = self._public_object_url(name)
            return f'{url}?{urlencode(parameters)}' if parameters else url

        params = {
            'Bucket': self.bucket_name,
            'Key': name,
        }
        if parameters:
            params.update(parameters)
        return self._public_boto_client().generate_presigned_url(
            'get_object',
            Params=params,
            ExpiresIn=expire or self.querystring_expire,
            HttpMethod=http_method or 'GET',
        )
