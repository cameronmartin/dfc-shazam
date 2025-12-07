FROM registry.access.redhat.com/ubi9/openjdk-21 AS builder

WORKDIR /app
COPY . .
RUN ./mvnw package -DskipTests

FROM registry.access.redhat.com/ubi9/openjdk-21-runtime

WORKDIR /app
COPY --from=builder /app/target/quarkus-app /app/

EXPOSE 8080
CMD ["java", "-jar", "quarkus-run.jar"]
