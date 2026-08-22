## EVAL DEFINITION: cinema-matcher-ab

### Population
- 100 independent recent buyer conversations whose latest quote event contains an image.
- One sample per tenant/account/chat; duplicate image events in the same conversation do not increase the denominator.
- Historical samples may be used only when a bounded recognition snapshot already exists. Images must not be re-recognized for replay.

### Baseline
Current production catalog/search plus joint official showtime matching.

### Candidate
1. Require explicit buyer city when recognition has no city.
2. Compare the recognized cinema title against every official cinema in that city.
3. Rank by normalized matching-character coverage and a minimum score margin; generic titles fail closed.
4. Keep bounded cinema candidates for exact official movie + absolute date + start-time resolution.
5. Use hall only as a deterministic tiebreak after movie/date/time; require one final candidate and confidence >= 0.85.

### Success criteria
- Unique official cinema accuracy against authoritative ground truth.
- Complete cinema + movie + date + start-time success rate.
- Wrong-city matches: 0.
- Wrong-showtime matches: 0.
- False unique matches: 0.
- Ask-city rate and unresolved rate reported separately, not counted as false success.
- Candidate must improve complete-path success without reducing authoritative accuracy.

### Required report
- Sample count and exclusions.
- Baseline and candidate success counts/rates with 95% Wilson intervals.
- Paired outcomes: both pass, baseline-only, candidate-only, both fail.
- Failure reasons and title-score distributions.
- Results split by explicit city, supplemented city, truncated title, and complete title.

### Release rule
This experiment is offline only. Do not replace production matching until 100 eligible samples exist and the candidate has no false unique/wrong-city/wrong-showtime result.
