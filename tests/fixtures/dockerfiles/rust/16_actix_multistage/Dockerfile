FROM rust:1.75 AS builder

WORKDIR /app
COPY . .
RUN cargo build --release

FROM debian:bookworm-slim

WORKDIR /app
COPY --from=builder /app/target/release/actix-app /app/

EXPOSE 8080
CMD ["./actix-app"]
