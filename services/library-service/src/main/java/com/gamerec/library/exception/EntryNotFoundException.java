package com.gamerec.library.exception;

/** Thrown by the service when an entry does not exist. The exception handler turns it into a 404. */
public class EntryNotFoundException extends RuntimeException {

    public EntryNotFoundException(String userId, int appId) {
        super("app " + appId + " is not in the library of user " + userId);
    }
}
