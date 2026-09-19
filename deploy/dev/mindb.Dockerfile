# Local development only.
#
# MinDB has no tagged release and no published GHCR image yet, so this builds the server from a pinned
# commit via the Go module proxy. It never touches a local MinDB checkout. Per D19, deployment must use
# the multi-arch GHCR image at a released tag; delete this file when that exists.
FROM golang:1.25-alpine AS build

# Keep in step with clients/README.md.
ARG MINDB_VERSION=v0.0.0-20260918114935-69a0c9c705c8
RUN CGO_ENABLED=0 go install github.com/typicallhavok/mindb/cmd/mindb-server@${MINDB_VERSION}

FROM alpine:3.21
COPY --from=build /go/bin/mindb-server /usr/local/bin/mindb-server
RUN adduser -D -u 10001 mindb && mkdir -p /data && chown mindb:mindb /data
USER mindb
VOLUME /data
EXPOSE 50051 50052
ENTRYPOINT ["mindb-server"]
