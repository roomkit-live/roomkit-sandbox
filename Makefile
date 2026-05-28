IMAGE  := ghcr.io/roomkit-live/sandbox
TAG    := latest

PLATFORMS := linux/amd64,linux/arm64

.PHONY: build push build-amd64 build-arm64

## Build & push multi-arch image to registry (requires docker buildx + login)
push:
	docker buildx build \
		--platform $(PLATFORMS) \
		-t $(IMAGE):$(TAG) \
		--push .

## Build single-arch image for local use
build-amd64:
	docker buildx build \
		--platform linux/amd64 \
		-t $(IMAGE):$(TAG) \
		--load .

build-arm64:
	docker buildx build \
		--platform linux/arm64 \
		-t $(IMAGE):$(TAG) \
		--load .

## Build for current host architecture
build:
	docker buildx build \
		-t $(IMAGE):$(TAG) \
		--load .
