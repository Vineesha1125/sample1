ILLEGAL_TEXT_CATEGORIES = {
    'weapons': (
        ('gun', 'firearm', 'pistol', 'rifle', 'weapon'),
        ('no license', 'unlicensed', 'unregistered', 'for sale', 'selling',
         'message me', 'no questions', 'cash only', 'untraceable'),
    ),
}

def score_illegal_text(text):
    lowered = text.lower()
    matches = []
    for category, (subject_terms, intent_terms) in ILLEGAL_TEXT_CATEGORIES.items():
        matched_subject = next((t for t in subject_terms if t in lowered), None)
        matched_intent = next((t for t in intent_terms if t in lowered), None)
        if matched_subject and matched_intent:
            matches.append(f"{category}: '{matched_subject}' + '{matched_intent}'")
    return matches

test_text = "Selling a gun, no license needed, message me"
print("Result:", score_illegal_text(test_text))