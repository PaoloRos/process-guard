# Process Guard Program

Implement a Guard that is controlling one or more process such that they are not using too much resources.

The aim of this program is to limit the computer wear, specifically during the training of CNN models.

Consider that I've a MacBookAir M2 with 16GB of RAM.

## What the program does

It monitors the [thermal pressure](https://developer.apple.com/library/archive/documentation/Performance/Conceptual/power_efficiency_guidelines_osx/RespondToThermalStateChanges.html) periodiccally: it must stay under a certain level decided by the `user`, by default the level of `nominal`.

When the process exceed that level, the guard should pause the process until the thermal pressure reaches the `nominal` level. Then the process could restart.

Moreover, the guard should save the process data into the HD, because it could happend that the computer crashes and the `user` will loose the achievements. ie: perfomr a backup.

I think thet the process should be launched by the guard in order that it can contrllo the process.

Suggest me any additional actions.