FEATURES = ("doorbell", "fridge", "vacuum", "hvac", "ice_maker", "matterbridge")
DOORBELL_FEATURES = {name: name == "doorbell" for name in FEATURES}


def feature_for_event(event):
    if event.startswith("doorbell"):
        return "doorbell"
    return event if event in FEATURES else None
