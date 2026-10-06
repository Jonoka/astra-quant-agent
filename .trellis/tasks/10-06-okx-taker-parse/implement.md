# Local implementation and verification

1. Preserve both existing checkouts and deployment/evidence directories; clone
   the clean deployed f53e579 base into a separate local repair branch.
2. Add shared pure row parsing, correct primary/SmartMoney direction, and guard
   factor/CVD/overlay missing data without changing acquisition or trade policy.
3. Add synthetic parser and end-to-end regression coverage. Update only the
   exact old sign/missing expectations. The extraction guard already fails on
   untouched f53e579; pin its full function to the deployed baseline and allow
   only the exact parser delta. Retain the old fixture as historical evidence.
4. Run affected modules in an existing local Linux image with no network, a
   read-only source mount, scratch data and network-denying test harness. Do not
   build, pull or publish an image. Verify package and existing script helper
   fallback imports; the rest of the application still needs its scripts package.
5. Review diff separately against official schema, negative-input cases and
   allowed scope; document remaining limits, then create a local commit only.

Rollback for this unshipped change is to leave the original clean checkout
untouched and decline the separate repair branch. No runtime rollback occurs.
