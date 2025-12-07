FROM golang:1.21 AS builder

WORKDIR /app

COPY go.mod ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -a -ldflags='-extldflags "-static"' -o /server .

FROM scratch

COPY --from=builder /server /server

EXPOSE 8080

ENTRYPOINT ["/server"]
