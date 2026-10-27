package com.gamerec.library.exception;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.validation.FieldError;
import org.springframework.web.ErrorResponse;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.HandlerMethodValidationException;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;

import java.util.List;

/**
 * Every error leaves as the same JSON shape: RFC 9457 "problem details", which Spring builds as
 * ProblemDetail and sends as application/problem+json --
 *
 *   {"type":"about:blank","title":"Bad Request","status":400,"detail":"...","instance":"/api/v1/..."}
 *
 * -- with an "errors" list added when there is more than one thing to say.
 */
@RestControllerAdvice
public class ApiExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ApiExceptionHandler.class);

    @ExceptionHandler(EntryNotFoundException.class)
    public ProblemDetail notFound(EntryNotFoundException e) {
        return ProblemDetail.forStatusAndDetail(HttpStatus.NOT_FOUND, e.getMessage());
    }

    /**
     * A constraint failed: on a path variable or query parameter (appId=0, size=500), or inside the
     * @Valid body (no "status"). Spring MVC validates the whole method at once whenever any parameter
     * carries a constraint, which every endpoint here does, so all of these arrive as this one
     * exception. A body field is named by its field, anything else by its parameter.
     */
    @ExceptionHandler(HandlerMethodValidationException.class)
    public ProblemDetail invalid(HandlerMethodValidationException e) {
        List<String> errors = e.getParameterValidationResults().stream()
                .flatMap(result -> result.getResolvableErrors().stream()
                        .map(error -> (error instanceof FieldError field
                                ? field.getField()
                                : result.getMethodParameter().getParameterName())
                                + ": " + error.getDefaultMessage()))
                .toList();
        return badRequest("the request is invalid", errors);
    }

    /** A parameter that cannot even be converted, e.g. appId=abc or status=BOUGHT in the query. */
    @ExceptionHandler(MethodArgumentTypeMismatchException.class)
    public ProblemDetail wrongType(MethodArgumentTypeMismatchException e) {
        return badRequest("the request is invalid",
                List.of(e.getName() + ": cannot be read as " + e.getRequiredType().getSimpleName()));
    }

    /** A body that is not JSON, or names a status that does not exist. */
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ProblemDetail unreadable(HttpMessageNotReadableException e) {
        return badRequest("the request body is not valid JSON for this endpoint, "
                + "or its status is not one of OWNED, WISHLIST", List.of());
    }

    /** Two requests created the same entry at once; the unique constraint stopped the second. */
    @ExceptionHandler(DataIntegrityViolationException.class)
    public ProblemDetail conflict(DataIntegrityViolationException e) {
        return ProblemDetail.forStatusAndDetail(HttpStatus.CONFLICT,
                "the entry was changed by another request at the same time; retry");
    }

    /**
     * Everything else. Spring's own exceptions (404 for an unknown path, 405 for a wrong method, ...)
     * implement ErrorResponse and already carry the right status and a ProblemDetail, so they are
     * passed through; without that, this catch-all would turn every one of them into a 500.
     * Anything left is a bug: logged with its stack trace, and not described to the client.
     */
    @ExceptionHandler(Exception.class)
    public ProblemDetail unexpected(Exception e) {
        if (e instanceof ErrorResponse springError) {
            return springError.getBody();
        }
        log.error("unhandled exception", e);
        return ProblemDetail.forStatusAndDetail(HttpStatus.INTERNAL_SERVER_ERROR, "internal error");
    }

    private static ProblemDetail badRequest(String detail, List<String> errors) {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(HttpStatus.BAD_REQUEST, detail);
        if (!errors.isEmpty()) {
            problem.setProperty("errors", errors);
        }
        return problem;
    }
}
