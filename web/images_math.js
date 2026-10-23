'use strict';

function panoramaTexel(direction)
{
    const [x, y, z] = direction;
    const u = 0.5 - Math.atan2(y, x) / (2 * Math.PI);
    return [u - Math.floor(u), 0.5 - Math.atan2(z, Math.hypot(x, y)) / Math.PI];
}

function viewDirection(azimuth, elevation)
{
    return [Math.cos(elevation) * Math.cos(azimuth), Math.cos(elevation) * Math.sin(azimuth), Math.sin(elevation)];
}

function cameraAxis(rotation)
{
    const degrees = 180 / Math.PI;
    return {
        azimuth: Math.atan2(rotation[7], rotation[6]) * degrees,
        elevation: Math.asin(Math.min(Math.max(rotation[8], -1), 1)) * degrees
    };
}

function cameraGroup(elevation)
{
    if (elevation < -10) return 'Down';
    if (elevation > 35) return 'Up';
    return 'Level';
}

function imageTabs(panorama)
{
    const exported = panorama || {};
    const panoramaReason = 'Convert with Export panoramas to view panoramas.';
    const cameraReason = 'Convert with Export camera images to view camera images.';
    const tab = (id, label, enabled, reason) => ({id, label, enabled, reason: enabled ? '' : reason});
    return [
        tab('colour', 'Colour panorama', Boolean(exported.colour_path), panoramaReason),
        tab('intensity', 'Intensity panorama', Boolean(exported.intensity_path), panoramaReason),
        tab('camera', exported.image_count ? `Camera images (${exported.image_count})` : 'Camera images', Boolean(exported.images_path), cameraReason)
    ];
}

function firstEnabledTab(tabs)
{
    const tab = tabs.find(item => item.enabled);
    return tab ? tab.id : null;
}

function flatAngles(u, v)
{
    return [(0.5 - u) * 360, (0.5 - v) * 180];
}

function keepsDecodedImage(key)
{
    return key === 'colour' || key === 'intensity';
}

function textureSize(width, height, limit)
{
    const shrink = Math.min(1, Math.min(limit, 8192) / Math.max(width, height));
    return [Math.floor(width * shrink), Math.floor(height * shrink)];
}
