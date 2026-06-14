#!/bin/bash
cd /home/ubuntu/_qoder/
# Build using root context, pointing to replica-mon/Containerfile
podman build -t localhost/replica-mon:latest -f replica-mon/Containerfile .
cd replica-mon
podman-compose up -d --force-recreate

