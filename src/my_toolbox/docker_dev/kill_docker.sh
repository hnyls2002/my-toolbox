#!/bin/bash
CONTAINER_NAME=${CONTAINER_NAME:-lsyin_sgl}
docker stop ${CONTAINER_NAME}
docker rm ${CONTAINER_NAME}
