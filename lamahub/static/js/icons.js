/**
 * Lamahub - Icon registration
 * @description Registers the app's Lucide icons with druids so markup can use
 * <druid-icon name="..."> and <druid-icon-button icon="...">. Loaded as a
 * module so it runs after druids.js has installed window.druids (registration
 * after upgrade is fine — druids re-renders icons on register).
 */
druids.registerIcons({
    "monitor-cog": `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <path d="M12 17v4"></path>
            <path d="m14.305 7.53.923-.382"></path>
            <path d="m15.228 4.852-.923-.383"></path>
            <path d="m16.852 3.228-.383-.924"></path>
            <path d="m16.852 8.772-.383.923"></path>
            <path d="m19.148 3.228.383-.924"></path>
            <path d="m19.53 9.696-.382-.924"></path>
            <path d="m20.772 4.852.924-.383"></path>
            <path d="m20.772 7.148.924.383"></path>
            <path d="M22 13v2a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"></path>
            <path d="M8 21h8"></path>
            <circle cx="18" cy="6" r="3"></circle>
        </svg>
    `,
    "brain": `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <path d="M12 18V5"></path>
            <path d="M15 13a4.17 4.17 0 0 1-3-4 4.17 4.17 0 0 1-3 4"></path>
            <path d="M17.598 6.5A3 3 0 1 0 12 5a3 3 0 1 0-5.598 1.5"></path>
            <path d="M17.997 5.125a4 4 0 0 1 2.526 5.77"></path>
            <path d="M18 18a4 4 0 0 0 2-7.464"></path>
            <path d="M19.967 17.483A4 4 0 1 1 12 18a4 4 0 1 1-7.967-.517"></path>
            <path d="M6 18a4 4 0 0 1-2-7.464"></path>
            <path d="M6.003 5.125a4 4 0 0 0-2.526 5.77"></path>
        </svg>
    `,
    "arrow-up-wide-narrow": `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <path d="m3 8 4-4 4 4"></path>
            <path d="M7 4v16"></path>
            <path d="M11 12h10"></path>
            <path d="M11 16h7"></path>
            <path d="M11 20h4"></path>
        </svg>
    `,
    "image": `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <rect width="18" height="18" x="3" y="3" rx="2" ry="2"></rect>
            <circle cx="9" cy="9" r="2"></circle>
            <path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"></path>
        </svg>
    `,
    "pin": `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <path d="M12 17v5"></path>
            <path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"></path>
        </svg>
    `,
    "rotate-cw": `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"></path>
            <path d="M21 3v5h-5"></path>
        </svg>
    `,
    "upload": `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <path d="M12 3v12"></path>
            <path d="m17 8-5-5-5 5"></path>
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        </svg>
    `,
    "x": `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <path d="M18 6 6 18"></path>
            <path d="m6 6 12 12"></path>
        </svg>
    `,
});
