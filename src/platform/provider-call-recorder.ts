/** DB-backed implementation of the LLM provider-call accounting port. */

import type { Database } from "../db/connection";
import {
    failProviderCall,
    finishProviderCall,
    startProviderCall,
} from "../db/queries/finance";
import type { ProviderCallRecorder } from "../llm/types";

export function createDbProviderCallRecorder(
    db: Database,
): ProviderCallRecorder {
    return {
        start: (input) => startProviderCall(db, input),
        finish: (id, input) => finishProviderCall(db, id, input),
        fail: (id, input) => failProviderCall(db, id, input),
    };
}
