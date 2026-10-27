# Two stages: a JDK and Maven to build the jar, then only a JRE to run it. The build tools never
# reach the image that ships.
#
#   docker buildx build --platform linux/amd64,linux/arm64 -t library-service .

# --platform=$BUILDPLATFORM runs this stage natively on whatever machine is building. The jar is
# bytecode and identical for every target, so compiling it under QEMU for arm64 would be slow for
# nothing; only the runtime stage below is built once per platform.
FROM --platform=$BUILDPLATFORM maven:3.9.16-eclipse-temurin-21 AS build
WORKDIR /src

# The pom alone first, so the dependency download is its own layer and is reused from cache until
# the pom changes -- which is far less often than the code does.
COPY pom.xml .
RUN mvn -B -q dependency:go-offline

COPY src ./src
# Tests run in CI before an image is built, so they are not run a second time here.
RUN mvn -B -q package -DskipTests


# The runtime stage. eclipse-temurin publishes this tag for both amd64 and arm64.
FROM eclipse-temurin:21-jre
WORKDIR /app

# A fixed, unprivileged uid. The Kubernetes manifest asserts runAsNonRoot and runs as this uid.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app

# The wildcard matches the repackaged jar and not its *.jar.original sibling.
COPY --from=build /src/target/library-service-*.jar app.jar

USER 10001
EXPOSE 8080

# The JVM sizes its heap from the container's memory limit. Half for the heap leaves the other half
# for metaspace, thread stacks and the JIT, which together are ~150 MB for a Spring Boot app; at the
# 512Mi limit in k8s/ that is a 256 MB heap and no OOM kill from the non-heap side.
ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=50"

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
