# Question-scoped local evidence interface v1

Existing resolve(...) defaults and local-evidence-resolution/v1 output contract remain compatible. Opt-in keyword `selection_policy` is a mapping:

- contract: question-scoped/v1
- assessment_kinds: nonempty list of producer-known exact assessment family keys
- current_statuses: nonempty list (consumer active/disputed)
- archived_statuses: disjoint nonempty list (consumer superseded/withdrawn)
- collection_tasks: exact marker -> {assessment_kinds, fields, question_kinds, required_context}; each list nonempty; required_context a nonempty mapping of exact string metadata keys/values.

Unsupported/malformed policy raises ValueError before reads. Task keys must be exact `collection-requirement:` identifiers with lowercase alphanumeric/hyphen suffix. Recognition also requires matched assessment family, reference field and ALL reference metadata required_context values. Prefixes, bare marker strings, wrong contexts cannot become tasks.

Question relevance is explicitly `question_kind` on the reference mapping. No text inference or assessment conclusion inference. Current matching family, direct actor references and unknown family/status remain load-bearing. Known unrelated current family or known archived reference is contextual, unless explicit matching question_kind overrides. An explicit question_kind outside the policy vocabulary (the asked question, `assessment_kinds`, and every task's `question_kinds`), including a non-string, is unknown and fails closed: the reference stays blocking with reason `unknown-question-kind` and is never demoted. An absent (null or empty) question_kind keeps the family rule. Draft/resolved/missing/unknown status remains load-bearing. Subject selection accepts exact subject or subject_ref canonical path; IDs are not arbitrary subject matches.

Additional result fields: selection_policy echoed; reference_diagnostics array. Each diagnostic has reference, origin, category, disposition, blocking (bool), reason, question_kind, assessment_kind, assessment_status, reference_context; documentary lookup also has resolution_reason. Categories documentary, contextual-provenance, collection-task, assessment-conclusion. Disposition blocking/contextual; recognized relevant tasks may block, separately from unread documents. Existing unresolved array contains ONLY blocking resolution failures; consumer ALSO must inspect blocking task diagnostics. Contextual readable documentary pages stay in resolved, preserving discovery. Assessment pages are not independent evidence. Ambiguous scoped URL/ID mappings do not select arbitrary sources. Metadata-only/alias scope stays unchanged for consumer qualification.

Scoped snapshot digest covers policy, question, subject digest, every resolved semantic field, every diagnostic/context/provenance/disposition, unresolved and malformed evidence, and alias policy; failed subject snapshot also records policy/question/subject.

Scoped duplicate artifacts retain exact canonical actor operator page-path provenance first, then current/direct load-bearing reference provenance, then the existing deterministic reference/origin order. This does not manufacture attestation: the source still must satisfy the unchanged operator-owned record predicate. ID/URL operator references remain refused. Every reference diagnostic is retained, and contextual-only readable independent documents remain discoverable. Blocking is a completeness axis; it is not a blanket positive-evidence eligibility rule. Default callers retain their original ordering.

Coverage consumer emits `reason_details` array using diagnostic entries plus domain-generated category `conflicting-country-claims` and producer read failure diagnostics. Existing state/reason/unresolved fields remain. Cockpit preserves terminal labels/buckets and assessed confidence/review, adding escaped structured reasons/reference origin to accessible terminal detail. Domain review-status dashboard owns full reason details.
