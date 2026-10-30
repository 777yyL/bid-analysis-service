import logging

from analyzer.app import create_app

service_logger = logging.getLogger("analyzer")
service_logger.setLevel(logging.INFO)
if not service_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    service_logger.addHandler(handler)
service_logger.propagate = False

app = create_app()
